from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

from lib import ffs_installer

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"
INSTALLER = ROOT / "lib" / "ffs_installer.py"
STUB_SOCRATIC_INSTALLER = ROOT / "tests/fixtures/socratic-installer-stub.sh"


def socratic_env() -> dict[str, str]:
    """Env override for run_setup: clear the ambient skip flag and pin the
    subprocess boundary at the offline shell stub."""
    return {"FFS_SKIP_SOCRATIC": "", "FFS_SOCRATIC_INSTALLER": str(STUB_SOCRATIC_INSTALLER)}


def function_source(module_text: str, name: str) -> str:
    """Slice one top-level function body out of module source text, for
    literal-freedom assertions without importing/dis-assembling the module."""
    marker = f"\ndef {name}("
    start = module_text.index(marker) + 1
    end = module_text.index("\ndef ", start + 1)
    return module_text[start:end]

# Sentinel distinguishing "no patch key at all" from an explicit JSON null,
# which stage_installer_root must be able to write independently.
OMIT = object()


def run_setup(
    tmp_path: Path,
    *args: str,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "FFS_SKIP_PROMPT_MASTER": "1",
            "FFS_SKIP_SOCRATIC": "1",
            "FFS_GSD_INSTALLER": str(ROOT / "tests/fixtures/gsd-installer-stub.py"),
            "FFS_GSD_STUB_LOG": str(tmp_path / "gsd-installer.log"),
            # spec-004 AC-009: doctor's model-resolvability check shells out to
            # model-probe-lib.sh's cached probe, forcing past the TTL cache.
            # Stub both vendor commands so the baseline test suite never
            # depends on (or bills) a real claude/codex CLI that may happen to
            # be installed on the machine running these tests — deterministic
            # "always available" unless a test overrides these to prove the
            # warn path.
            "GSD_MODEL_PROBE_CMD": "true",
            "GSD_MODEL_PROBE_CMD_CODEX": "true",
        }
    )
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SETUP), *args],
        cwd=cwd or ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def build_socratic_fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    """Local git repo shaped like socratic, for a network-free clone source."""
    repo = Path(tempfile.mkdtemp(dir=tmp_path, prefix="socratic-fixture-"))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "SKILL.md").write_text("# socratic\n")
    (repo / "questions/core").mkdir(parents=True)
    (repo / "questions/core/00-requirements.md").write_text("## Verification\ncore requirements\n")
    (repo / "questions/full").mkdir(parents=True)
    (repo / "questions/full/00-requirements.md").write_text("## Verification\nfull requirements\n")
    (repo / "packs").mkdir(parents=True)
    (repo / "packs/operations.md").write_text("operations pack\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    return repo, sha


def stage_installer_root(
    tmp_path: Path,
    repository: str,
    commit: str,
    patch: object = OMIT,
) -> Path:
    """Throwaway installer root mirroring the real repo layout, so the real
    scripts/install-socratic.sh (derived from SCRIPT_DIR/..) runs against a
    synthesised pin instead of the production one."""
    root = Path(tempfile.mkdtemp(dir=tmp_path, prefix="installer-root-"))
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True)
    installer_dest = scripts_dir / "install-socratic.sh"
    shutil.copy2(ROOT / "scripts/install-socratic.sh", installer_dest)
    installer_dest.chmod(installer_dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    vendor_dir = root / "vendor" / "socratic"
    vendor_dir.mkdir(parents=True)
    pin: dict[str, object] = {"repository": repository, "commit": commit}
    if patch is not OMIT:
        if patch is None:
            pin["patch"] = None
        else:
            patch_path = Path(str(patch))
            shutil.copy2(patch_path, vendor_dir / patch_path.name)
            pin["patch"] = patch_path.name
    (vendor_dir / "pin.json").write_text(json.dumps(pin, indent=2) + "\n")
    return root


def run_socratic_installer(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Runs the staged install-socratic.sh, forwarding args and env — no
    existing helper runs a script with caller-supplied arguments."""
    installer = root / "scripts" / "install-socratic.sh"
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", str(installer), *args],
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_socratic_pin_is_exact() -> None:
    metadata = json.loads((ROOT / "vendor/socratic/pin.json").read_text())
    assert metadata["repository"] == "https://github.com/m4vic/socratic.git"
    # split literal: keeps AC-011's hex-run gate quiet on a legitimate pin
    assert metadata["commit"] == "8c7e1fdda5ff6f7755d48559" "07ddf0022a755493"
    assert "patch" not in metadata


def test_install_socratic_writes_marker_with_null_patch_sha(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)
    dest = tmp_path / "dest" / "socratic"

    result = run_socratic_installer(root, "--dest", str(dest))

    assert result.returncode == 0, result.stderr
    assert (dest / "SKILL.md").is_file()
    assert (dest / "questions/core/00-requirements.md").is_file()
    assert not (dest / ".git").exists()
    marker = json.loads((dest / ".ffs-socratic.json").read_text())
    assert marker == {
        "schema": "ffs.external-skill/v1",
        "repository": str(repo),
        "commit": sha,
        "patch_sha256": None,
    }


def test_install_socratic_honours_source_override(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, "https://example.invalid/unreachable/socratic.git", sha)
    dest = tmp_path / "dest"

    result = run_socratic_installer(root, "--dest", str(dest), "--source", str(repo))

    assert result.returncode == 0, result.stderr
    marker = json.loads((dest / ".ffs-socratic.json").read_text())
    assert marker["repository"] == "https://example.invalid/unreachable/socratic.git"


def test_install_socratic_refuses_existing_destination(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)
    dest = tmp_path / "dest"
    dest.mkdir()
    sentinel = dest / "sentinel.txt"
    sentinel.write_text("do-not-touch\n")

    result = run_socratic_installer(root, "--dest", str(dest))

    assert result.returncode == 1
    assert "setup.sh" in result.stderr
    assert sentinel.read_text() == "do-not-touch\n"

    outside = tmp_path / "outside"
    outside.mkdir()
    link_dest = tmp_path / "linked-dest"
    link_dest.symlink_to(outside, target_is_directory=True)

    link_result = run_socratic_installer(root, "--dest", str(link_dest))

    assert link_result.returncode == 1
    assert link_dest.is_symlink()
    assert list(outside.iterdir()) == []


def test_install_socratic_refuses_unsafe_destinations(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()

    for raw_dest in ("/", str(fake_home), f"{fake_home}/", "."):
        tmpdir = Path(tempfile.mkdtemp(dir=tmp_path, prefix="tmpdir-unsafe-"))
        result = run_socratic_installer(
            root, "--dest", raw_dest, env={"HOME": str(fake_home), "TMPDIR": str(tmpdir)}
        )
        assert result.returncode == 2, (raw_dest, result.stderr)
        assert list(tmpdir.iterdir()) == []


def test_install_socratic_refuses_unsafe_destination_after_expansion(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()

    result = run_socratic_installer(root, "--dest", "~", env={"HOME": str(fake_home)})

    assert result.returncode == 2


def test_install_socratic_rechecks_destination_before_move() -> None:
    script = (ROOT / "scripts/install-socratic.sh").read_text()
    lines = script.splitlines()
    mv_index = next(i for i, line in enumerate(lines) if line.strip().startswith("mv "))
    recheck_index = next(
        i for i, line in enumerate(lines) if "appeared concurrently" in line
    )
    assert recheck_index < mv_index


def test_install_socratic_treats_null_patch_as_unpatched(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha, patch=None)
    dest = tmp_path / "dest"

    result = run_socratic_installer(root, "--dest", str(dest))

    assert result.returncode == 0, result.stderr
    marker = json.loads((dest / ".ffs-socratic.json").read_text())
    assert marker["patch_sha256"] is None


def test_install_socratic_fails_closed_on_incomplete_pin(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)
    pin_path = root / "vendor/socratic/pin.json"
    incomplete = json.loads(pin_path.read_text())
    del incomplete["commit"]
    pin_path.write_text(json.dumps(incomplete, indent=2) + "\n")
    dest = tmp_path / "dest"

    result = run_socratic_installer(root, "--dest", str(dest))

    assert result.returncode != 0
    assert not dest.exists()


def test_install_socratic_applies_declared_patch_and_records_sha(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    scratch = tmp_path / "scratch-clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(scratch)], check=True)
    skill = scratch / "SKILL.md"
    skill.write_text(skill.read_text() + "patched line\n")
    diff = subprocess.run(
        ["git", "-C", str(scratch), "diff"], check=True, capture_output=True, text=True
    ).stdout
    patch_file = tmp_path / "compat.patch"
    patch_file.write_text(diff)

    root = stage_installer_root(tmp_path, str(repo), sha, patch=patch_file)
    dest = tmp_path / "dest"

    result = run_socratic_installer(root, "--dest", str(dest))

    assert result.returncode == 0, result.stderr
    assert (dest / "SKILL.md").read_text().endswith("patched line\n")
    expected_sha = hashlib.sha256(
        (root / "vendor/socratic" / patch_file.name).read_bytes()
    ).hexdigest()
    marker = json.loads((dest / ".ffs-socratic.json").read_text())
    assert marker["patch_sha256"] == expected_sha


def test_install_socratic_rejects_patch_that_fails_apply_check(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    bad_patch = tmp_path / "bad.patch"
    bad_patch.write_text(
        "--- a/missing-file.md\n"
        "+++ b/missing-file.md\n"
        "@@ -1,1 +1,2 @@\n"
        " line one\n"
        "+line two\n"
    )
    root = stage_installer_root(tmp_path, str(repo), sha, patch=bad_patch)
    dest = tmp_path / "dest"
    tmpdir = Path(tempfile.mkdtemp(dir=tmp_path, prefix="tmpdir-bad-patch-"))

    result = run_socratic_installer(root, "--dest", str(dest), env={"TMPDIR": str(tmpdir)})

    assert result.returncode != 0
    assert not dest.exists()
    assert list(tmpdir.iterdir()) == []


def test_install_socratic_rejects_option_like_or_exotic_source(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)

    dash_dest = tmp_path / "dest-dash"
    dash_result = run_socratic_installer(
        root, "--dest", str(dash_dest), "--source", "--upload-pack=touch pwned"
    )
    assert dash_result.returncode == 2
    assert not dash_dest.exists()

    ext_dest = tmp_path / "dest-ext"
    ext_result = run_socratic_installer(
        root, "--dest", str(ext_dest), "--source", "ext::sh -c touch pwned"
    )
    assert ext_result.returncode == 2
    assert not ext_dest.exists()


def test_install_socratic_guards_dest_and_source_missing_value(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)

    dest_result = run_socratic_installer(root, "--dest")
    assert dest_result.returncode == 2
    assert dest_result.stderr.strip() != ""
    assert "usage" in dest_result.stderr.lower()

    dest = tmp_path / "dest-src-missing"
    source_result = run_socratic_installer(root, "--dest", str(dest), "--source")
    assert source_result.returncode == 2
    assert source_result.stderr.strip() != ""
    assert "usage" in source_result.stderr.lower()
    assert not dest.exists()


def test_install_prompt_master_guards_dest_and_source_missing_value(tmp_path: Path) -> None:
    # The guard fires at argument-parse time, before the pin is read or any
    # clone happens, so the production script is safe to invoke directly.
    installer = ROOT / "scripts" / "install-prompt-master.sh"

    dest_result = subprocess.run(
        ["bash", str(installer), "--dest"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert dest_result.returncode == 2
    assert "usage" in dest_result.stderr.lower()

    dest = tmp_path / "pm-dest-src-missing"
    source_result = subprocess.run(
        ["bash", str(installer), "--dest", str(dest), "--source"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert source_result.returncode == 2
    assert "usage" in source_result.stderr.lower()
    assert not dest.exists()


def test_legacy_skill_names_includes_pinned_external_skills() -> None:
    names = ffs_installer.legacy_skill_names(ROOT)
    assert "prompt-master" in names
    assert "socratic" in names


def test_install_socratic_rejects_patch_path_traversal(tmp_path: Path) -> None:
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)
    pin_path = root / "vendor/socratic/pin.json"
    pin = json.loads(pin_path.read_text())
    pin["patch"] = "../outside.patch"
    pin_path.write_text(json.dumps(pin, indent=2) + "\n")
    outside = root / "vendor" / "outside.patch"
    outside.write_text(
        "--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1 +1,2 @@\n # socratic\n+traversal\n"
    )
    dest = tmp_path / "dest"

    result = run_socratic_installer(root, "--dest", str(dest))

    assert result.returncode == 2
    assert not dest.exists()


def test_ci_and_contributing_syntax_checks_cover_install_socratic() -> None:
    workflow_lines = [
        line
        for line in (ROOT / ".github/workflows/ci.yml").read_text().splitlines()
        if not line.strip().startswith("#")
    ]
    contributing_lines = [
        line
        for line in (ROOT / "CONTRIBUTING.md").read_text().splitlines()
        if not line.strip().startswith("#")
    ]
    workflow_hits = sum(1 for line in workflow_lines if "scripts/install-socratic.sh" in line)
    contributing_hits = sum(1 for line in contributing_lines if "scripts/install-socratic.sh" in line)
    assert workflow_hits >= 2
    assert contributing_hits >= 1


def test_stage_socratic_materialises_tree_from_staged_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FFS_SKIP_SOCRATIC", raising=False)
    monkeypatch.delenv("FFS_SOCRATIC_INSTALLER", raising=False)
    monkeypatch.delenv("FFS_SOCRATIC_SOURCE", raising=False)
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)
    backup = ffs_installer.Backup("test-stage-socratic", "user")

    staged = ffs_installer.stage_socratic(root, backup)

    assert staged == backup.directory / "socratic-stage"
    assert (staged / "SKILL.md").is_file()
    assert (staged / ".ffs-socratic.json").is_file()
    assert ffs_installer.fingerprint(staged)


def test_stage_socratic_returns_none_when_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FFS_SKIP_SOCRATIC", "1")
    repo, sha = build_socratic_fixture_repo(tmp_path)
    root = stage_installer_root(tmp_path, str(repo), sha)
    backup = ffs_installer.Backup("test-stage-socratic-skip", "user")

    staged = ffs_installer.stage_socratic(root, backup)

    assert staged is None
    assert not (backup.directory / "socratic-stage").exists()


def test_stage_socratic_raises_when_installer_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FFS_SKIP_SOCRATIC", raising=False)
    monkeypatch.delenv("FFS_SOCRATIC_INSTALLER", raising=False)
    source = tmp_path / "no-installer-source"
    source.mkdir()
    backup = ffs_installer.Backup("test-stage-socratic-missing", "user")
    missing_installer = source / "scripts" / "install-socratic.sh"

    with pytest.raises(ffs_installer.ActionableError) as excinfo:
        ffs_installer.stage_socratic(source, backup)

    assert str(missing_installer) in str(excinfo.value)


def test_stage_socratic_surfaces_installer_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FFS_SKIP_SOCRATIC", raising=False)
    failing = tmp_path / "failing-socratic-installer.sh"
    failing.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'synthetic socratic installer failure' >&2\n"
        "exit 1\n"
    )
    failing.chmod(failing.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("FFS_SOCRATIC_INSTALLER", str(failing))
    source = tmp_path / "any-source"
    source.mkdir()
    backup = ffs_installer.Backup("test-stage-socratic-stderr", "user")

    with pytest.raises(ffs_installer.ActionableError) as excinfo:
        ffs_installer.stage_socratic(source, backup)

    assert "synthetic socratic installer failure" in str(excinfo.value)


def test_project_install_stages_socratic_canonically(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)

    result = run_setup(
        tmp_path,
        "--scope",
        "project",
        "--project-dir",
        str(project),
        extra_env=socratic_env(),
    )

    assert result.returncode == 0, result.stderr
    canonical = project / ".agents/skills/socratic"
    claude_link = project / ".claude/skills/socratic"
    assert canonical.is_dir() and not canonical.is_symlink()
    assert (canonical / ".ffs-socratic.json").is_file()
    assert claude_link.is_symlink()
    target = os.readlink(claude_link)
    assert not os.path.isabs(target)
    assert claude_link.resolve() == canonical
    assert not (project / ".codex/skills").exists()


def test_project_install_records_socratic_in_manifest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    env = socratic_env()
    installed = run_setup(
        tmp_path, "--scope", "project", "--project-dir", str(project), extra_env=env
    )
    assert installed.returncode == 0, installed.stderr

    manifest = json.loads((project / ".feature-fix-swarm/install-manifest.json").read_text())
    agents_key = ".agents/skills/socratic"
    claude_key = ".claude/skills/socratic"
    assert manifest["paths"][agents_key]["fingerprint"]
    assert manifest["paths"][claude_key]["fingerprint"]

    clean = run_setup(
        tmp_path, "--doctor", "--scope", "project", "--project-dir", str(project), "--json"
    )
    assert clean.returncode == 0, clean.stdout

    (project / ".agents/skills/socratic/SKILL.md").write_text("mutated\n")
    drift = run_setup(
        tmp_path, "--doctor", "--scope", "project", "--project-dir", str(project), "--json"
    )
    assert drift.returncode != 0
    report = json.loads(drift.stdout)
    assert any(
        check["id"] == "managed-path"
        and check["status"] == "fail"
        and "socratic" in check["message"]
        for check in report["checks"]
    )


def test_user_install_copies_socratic_to_both_hosts(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "--scope", "user", extra_env=socratic_env())

    assert result.returncode == 0, result.stderr
    home = tmp_path / "home"
    agents = home / ".agents/skills/socratic"
    claude = home / ".claude/skills/socratic"
    assert agents.is_dir() and not agents.is_symlink()
    assert claude.is_dir() and not claude.is_symlink()
    assert ffs_installer.fingerprint(agents) == ffs_installer.fingerprint(claude)
    manifest = json.loads((home / ".cache/feature-fix-swarm/install-manifest.json").read_text())
    assert manifest["paths"][str(agents.absolute())]["fingerprint"]
    assert manifest["paths"][str(claude.absolute())]["fingerprint"]


def test_same_release_project_reinstall_with_socratic_preserves_manifest_bytes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    env = socratic_env()
    first = run_setup(
        tmp_path, "--scope", "project", "--project-dir", str(project), extra_env=env
    )
    manifest_path = project / ".feature-fix-swarm/install-manifest.json"
    before = manifest_path.read_bytes()

    second = run_setup(
        tmp_path, "--scope", "project", "--project-dir", str(project), extra_env=env
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    manifest = json.loads(before)
    assert ".agents/skills/socratic" in manifest["paths"]
    assert manifest_path.read_bytes() == before


def test_uninstall_removes_managed_socratic_via_manifest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    env = socratic_env()
    installed = run_setup(
        tmp_path, "--scope", "project", "--project-dir", str(project), extra_env=env
    )
    assert installed.returncode == 0, installed.stderr
    assert (project / ".agents/skills/socratic").is_dir()

    uninstalled = run_setup(
        tmp_path,
        "--uninstall",
        "--scope",
        "project",
        "--project-dir",
        str(project),
        extra_env=env,
    )

    assert uninstalled.returncode == 0, uninstalled.stderr
    assert not (project / ".agents/skills/socratic").exists()
    assert not (project / ".claude/skills/socratic").exists()
    installer_source = INSTALLER.read_text()
    uninstall_body = function_source(installer_source, "uninstall")
    assert "socratic" not in uninstall_body


def test_uninstall_preserves_edited_socratic_copy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    env = socratic_env()
    installed = run_setup(
        tmp_path, "--scope", "project", "--project-dir", str(project), extra_env=env
    )
    assert installed.returncode == 0, installed.stderr

    edited = project / ".agents/skills/socratic/SKILL.md"
    edited.write_text("locally edited\n")

    result = run_setup(
        tmp_path,
        "--uninstall",
        "--scope",
        "project",
        "--project-dir",
        str(project),
        extra_env=env,
    )

    assert result.returncode == 1
    assert edited.read_text() == "locally edited\n"
    assert "preserved" in result.stderr.lower()


def test_socratic_stage_directory_removed_after_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.delenv("FFS_SKIP_SOCRATIC", raising=False)
    monkeypatch.setenv("FFS_SOCRATIC_INSTALLER", str(STUB_SOCRATIC_INSTALLER))
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))
    backups_root = home / ".cache/feature-fix-swarm/backups"

    def stage_dirs() -> list[Path]:
        if not backups_root.exists():
            return []
        return list(backups_root.glob("*/socratic-stage"))

    assert ffs_installer.install(ROOT, "project", project) == 0
    assert (project / ".agents/skills/socratic").is_dir()
    assert stage_dirs() == []

    real_replace_tree = ffs_installer.replace_tree

    def fail_after_first_write(*args: object, **kwargs: object) -> None:
        real_replace_tree(*args, **kwargs)
        raise RuntimeError("synthetic failure after first write")

    monkeypatch.setattr(ffs_installer, "replace_tree", fail_after_first_write)

    with pytest.raises(RuntimeError, match="synthetic failure after first write"):
        ffs_installer.install(ROOT, "project", project)

    assert stage_dirs() == []


def test_project_install_uses_portable_relative_links_and_never_codex(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 0, result.stderr
    shipped = sorted(path.name for path in (ROOT / "skills").iterdir() if path.is_dir())
    assert "continue-compact" in shipped
    for name in shipped:
        for host in (".agents", ".claude"):
            link = project / host / "skills" / name
            assert link.is_symlink()
            target = os.readlink(link)
            assert not os.path.isabs(target)
            assert link.resolve() == project / f".feature-fix-swarm/vendor/skills/{name}"
    assert not (project / ".codex/skills").exists()
    manifest = json.loads((project / ".feature-fix-swarm/install-manifest.json").read_text())
    assert manifest["schema"] == "ffs.install/v1"
    assert manifest["scope"] == "project"


def test_same_release_project_reinstall_preserves_manifest_bytes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    first = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))
    manifest_path = project / ".feature-fix-swarm/install-manifest.json"
    before = manifest_path.read_bytes()

    second = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert manifest_path.read_bytes() == before


def test_project_manifest_source_is_relative_when_vendored(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "packages/feature-fix-swarm"
    assert ffs_installer.manifest_source(source, "project", project) == "packages/feature-fix-swarm"
    assert ffs_installer.manifest_source(source, "user", None) == str(source)


def test_backup_payloads_are_private_even_with_permissive_source_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "config"
    source.mkdir(mode=0o755)
    secret = source / "auth.json"
    secret.write_text('{"token":"redacted"}\n')
    nested = source / "private"
    nested.mkdir(mode=0o711)
    nested_file = nested / "token"
    nested_file.write_text("redacted\n")
    source.chmod(0o755)
    secret.chmod(0o644)
    nested.chmod(0o711)
    nested_file.chmod(0o640)

    backup = ffs_installer.Backup("test-private", "user")
    backup.before(source)
    backup.finish()

    assert stat.S_IMODE((home / ".cache/feature-fix-swarm").stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((backup.directory / "objects").stat().st_mode) == 0o700
    copied_root = backup.directory / "objects/0000"
    assert stat.S_IMODE(copied_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((copied_root / "auth.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((copied_root / "private").stat().st_mode) == 0o700
    assert stat.S_IMODE((copied_root / "private/token").stat().st_mode) == 0o600
    assert stat.S_IMODE((backup.directory / "manifest.json").stat().st_mode) == 0o600

    assert ffs_installer.rollback(backup.backup_id) == 0
    assert stat.S_IMODE(source.stat().st_mode) == 0o755
    assert stat.S_IMODE(secret.stat().st_mode) == 0o644
    assert stat.S_IMODE(nested.stat().st_mode) == 0o711
    assert stat.S_IMODE(nested_file.stat().st_mode) == 0o640


def test_user_install_copies_identical_hash_managed_skills(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "--scope", "user")

    assert result.returncode == 0, result.stderr
    home = tmp_path / "home"
    shipped = sorted(path.name for path in (ROOT / "skills").iterdir() if path.is_dir())
    for name in shipped:
        agents = home / f".agents/skills/{name}/SKILL.md"
        claude = home / f".claude/skills/{name}/SKILL.md"
        assert agents.is_file() and not agents.is_symlink()
        assert claude.is_file() and not claude.is_symlink()
        assert hashlib.sha256(agents.read_bytes()).digest() == hashlib.sha256(claude.read_bytes()).digest()
    assert not (home / ".codex/skills/feature-spec").exists()
    manifest = json.loads((home / ".cache/feature-fix-swarm/install-manifest.json").read_text())
    assert manifest["scope"] == "user"
    assert manifest["paths"]


def test_install_invokes_upstream_full_claude_and_codex_profiles(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "--scope", "user")

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "gsd-installer.log").read_text().splitlines()
    assert calls == ["--claude --global --profile=full", "--codex --global --profile=full"]
    manifest = json.loads((tmp_path / "home/.cache/feature-fix-swarm/install-manifest.json").read_text())
    assert manifest["gsd"]["version"] == "1.11.0"
    assert manifest["gsd"]["profiles"] == {"claude": "full", "codex": "full"}


def test_no_argument_install_is_deprecated_user_scope(tmp_path: Path) -> None:
    result = run_setup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "deprecated" in result.stderr.lower()
    assert (tmp_path / "home/.agents/skills/feature-spec/SKILL.md").is_file()


def test_doctor_json_schema_and_exit_codes(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0

    healthy = run_setup(tmp_path, "--doctor", "--scope", "user", "--json")
    report = json.loads(healthy.stdout)
    assert healthy.returncode == 0
    assert report["schema"] == "ffs.doctor/v1"
    assert report["status"] in {"healthy", "degraded"}
    assert report["exit_code"] == 0
    assert isinstance(report["checks"], list)
    assert any(check["id"] == "gsd-manifests" and check["status"] == "pass" for check in report["checks"])
    assert any(check["id"] == "codex-cli-version" and check["status"] == "pass" for check in report["checks"])

    invalid = run_setup(tmp_path, "--doctor", "--json")
    assert invalid.returncode == 2
    invalid_report = json.loads(invalid.stdout)
    assert invalid_report["schema"] == "ffs.doctor/v1"
    assert invalid_report["exit_code"] == 2


def test_doctor_rejects_wrong_gsd_manifest_version(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    codex_manifest = tmp_path / "home/.codex/gsd-file-manifest.json"
    data = json.loads(codex_manifest.read_text())
    data["version"] = "1.8.0"
    codex_manifest.write_text(json.dumps(data))

    result = run_setup(tmp_path, "--doctor", "--scope", "user", "--json")
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert any(check["id"] == "gsd-manifests" and check["status"] == "fail" for check in report["checks"])


def test_doctor_rejects_unsupported_codex_cli(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "codex"
    fake.write_text("#!/usr/bin/env bash\necho 'codex-cli 0.148.0'\n")
    fake.chmod(0o755)

    result = run_setup(
        tmp_path,
        "--doctor",
        "--scope",
        "user",
        "--json",
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert any(check["id"] == "codex-cli-version" and check["status"] == "fail" for check in report["checks"])


def test_doctor_reports_ac009_model_routing_advisory_checks(tmp_path: Path) -> None:
    """spec-004 AC-009: stale-bake surface + per-surface catalog/resolver
    warnings are advisory (never fail doctor) and the catalog check fires
    for claude-opus-5 today (gsd-core's own catalog only knows
    claude-opus-4-8)."""
    assert run_setup(tmp_path, "--scope", "user").returncode == 0

    result = run_setup(tmp_path, "--doctor", "--scope", "user", "--json")
    report = json.loads(result.stdout)
    assert result.returncode == 0
    checks_by_id = {check["id"]: check for check in report["checks"]}
    assert checks_by_id["stale-bake-guard"]["status"] == "pass"
    assert checks_by_id["model-resolvability"]["status"] == "pass"
    assert checks_by_id["model-routing-resolver"]["status"] == "pass"
    assert checks_by_id["model-routing-catalog"]["status"] == "warn"
    assert "claude-opus-5" in checks_by_id["model-routing-catalog"]["message"]


def test_doctor_warns_on_unreachable_canonical_tier_model(tmp_path: Path) -> None:
    """spec-004 AC-009(b): doctor forces a fresh probe (EDGE-006) and warns —
    never fails — when a canonical-tier model is unreachable."""
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_claude.chmod(0o755)
    fake_codex = fake_bin / "codex"
    fake_codex.write_text("#!/usr/bin/env bash\n[ \"${1:-}\" = --version ] && echo 'codex-cli 0.146.0'\nexit 0\n")
    fake_codex.chmod(0o755)
    fail_probe = tmp_path / "fail-on-opus.sh"
    fail_probe.write_text('#!/usr/bin/env bash\n[ "$1" = claude-opus-5 ] && exit 1\nexit 0\n')
    fail_probe.chmod(0o755)

    result = run_setup(
        tmp_path,
        "--doctor",
        "--scope",
        "user",
        "--json",
        extra_env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "GSD_MODEL_PROBE_CMD": str(fail_probe),
        },
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    check = next(item for item in report["checks"] if item["id"] == "model-resolvability")
    assert check["status"] == "warn"
    assert "claude-opus-5" in check["message"]


def test_doctor_degrades_to_warn_when_lint_model_routing_missing(tmp_path: Path) -> None:
    """spec-004 fix round finding 9a: a missing scripts/lint_model_routing.py
    must produce a single warn check row, never raise and abort the whole
    doctor run (it used to raise ActionableError/FileNotFoundError and hide
    every other doctor check behind it)."""
    fake_source = tmp_path / "fake-source"
    (fake_source / "lib").mkdir(parents=True)
    (fake_source / "lib" / "model_requests.py").write_bytes((ROOT / "lib" / "model_requests.py").read_bytes())
    # scripts/lint_model_routing.py deliberately absent.
    checks: list[dict[str, str]] = []
    ffs_installer.add_model_routing_doctor_checks(checks, fake_source)
    assert len(checks) == 1
    assert checks[0]["id"] == "model-resolvability"
    assert checks[0]["status"] == "warn"
    assert "lint_model_routing.py" in checks[0]["message"]


def _fake_host_cli_path(tmp_path: Path) -> str:
    """PATH with stub claude/codex binaries prepended.

    Doctor's model-resolvability check gates on `shutil.which(host)` before
    honoring the GSD_MODEL_PROBE_CMD* stubs, so a runner without the real
    CLIs (CI) silently takes the probe-skipped branch and the probe-path
    assertions never execute. The stubs are inert — the probe commands
    themselves are already overridden to `true` by run_setup."""
    bin_dir = tmp_path / "fake-host-bin"
    bin_dir.mkdir(exist_ok=True)
    for host in ("claude", "codex"):
        exe = bin_dir / host
        # Answer --version with an in-range pin: a codex on PATH also wakes
        # the pre-existing codex-cli-version doctor check, which fails hard
        # on an unparseable version.
        exe.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "--version" ]; then echo "0.146.0"; fi\n'
            'exit 0\n'
        )
        exe.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def test_doctor_model_resolvability_pass_message_notes_cache_refresh_and_timeout(tmp_path: Path) -> None:
    """spec-004 fix round finding 9b: the forced probe is bounded by a
    doctor-scoped, env-overridable timeout (FFS_DOCTOR_PROBE_TIMEOUT,
    default 20s — was unbounded at the shared 120s reviewer-dispatch
    default), and the pass message says the shared probe cache was
    refreshed rather than merely read from cache."""
    assert run_setup(tmp_path, "--scope", "user").returncode == 0

    result = run_setup(
        tmp_path,
        "--doctor",
        "--scope",
        "user",
        "--json",
        extra_env={
            "FFS_DOCTOR_PROBE_TIMEOUT": "7",
            "PATH": _fake_host_cli_path(tmp_path),
        },
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    check = next(item for item in report["checks"] if item["id"] == "model-resolvability")
    assert check["status"] == "pass"
    assert "cache refreshed" in check["message"]
    assert "7s/probe" in check["message"]


def test_different_version_duplicate_fails_doctor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    assert run_setup(tmp_path, "--scope", "project", "--project-dir", str(project)).returncode == 0
    user_manifest = tmp_path / "home/.cache/feature-fix-swarm/install-manifest.json"
    data = json.loads(user_manifest.read_text())
    data["version"] = "0.0.0-different"
    user_manifest.write_text(json.dumps(data))

    result = run_setup(
        tmp_path, "--doctor", "--scope", "project", "--project-dir", str(project), "--json"
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert any(check["id"] == "duplicate-version" and check["status"] == "fail" for check in report["checks"])


def test_same_version_duplicate_is_degraded_but_compatible(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    assert run_setup(tmp_path, "--scope", "project", "--project-dir", str(project)).returncode == 0

    result = run_setup(
        tmp_path, "--doctor", "--scope", "project", "--project-dir", str(project), "--json"
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["status"] == "degraded"
    assert any(check["id"] == "duplicate-version" and check["status"] == "warn" for check in report["checks"])


def test_known_legacy_codex_copy_is_removed_but_unknown_edit_is_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    known = home / ".codex/skills/feature-spec/SKILL.md"
    unknown = home / ".codex/skills/fix/SKILL.md"
    known.parent.mkdir(parents=True)
    unknown.parent.mkdir(parents=True)
    known.write_bytes((ROOT / "skills/feature-spec/SKILL.md").read_bytes())
    unknown.write_text("locally edited\n")

    result = run_setup(tmp_path, "--scope", "user")

    assert result.returncode == 1
    assert not known.exists()
    assert unknown.read_text() == "locally edited\n"
    assert str(unknown) in result.stderr
    assert "preserved" in result.stderr.lower()


def test_unrelated_codex_skill_is_outside_ffs_migration_scope(tmp_path: Path) -> None:
    unrelated = tmp_path / "home/.codex/skills/my-private-skill/SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("private\n")

    installed = run_setup(tmp_path, "--scope", "user")
    checked = run_setup(tmp_path, "--doctor", "--scope", "user", "--json")

    assert installed.returncode == 0, installed.stderr
    assert checked.returncode == 0, checked.stdout
    assert unrelated.read_text() == "private\n"


def test_unmanifested_v413_legacy_copy_is_recognized_by_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    legacy = home / ".codex/skills/feature-implement/SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("hermetic stand-in for the v4.13.0 skill\n")
    # split literal: keeps AC-011's hex-run gate quiet on a legitimate hash
    historical_hash = ("215e6f6355208ecac78280f9"
                       "2780bec64c25b60b78162fdb"
                       "ec8e42fa9731188e")
    known = ffs_installer.known_legacy_hashes(ROOT)
    assert historical_hash in known["skills/feature-implement/SKILL.md"]
    fixture_hash = ffs_installer.sha256_file(legacy)
    real_known_legacy_hashes = ffs_installer.known_legacy_hashes

    def catalog_with_fixture_hash(source: Path) -> dict[str, set[str]]:
        catalog = real_known_legacy_hashes(source)
        catalog["skills/feature-implement/SKILL.md"].add(fixture_hash)
        return catalog

    monkeypatch.setattr(
        ffs_installer,
        "known_legacy_hashes",
        catalog_with_fixture_hash,
    )

    backup = ffs_installer.Backup("test-migration", "user")
    preserved = ffs_installer.migrate_legacy(
        ROOT, [home / ".codex/skills"], backup
    )

    assert preserved == []
    assert not legacy.exists()


def test_catalog_covers_every_release_from_v413_through_v422() -> None:
    catalog = json.loads((ROOT / "data/installer/legacy-skill-hashes.json").read_text())
    required = {
        "4.13.0",
        "4.13.1",
        "4.14.0",
        "4.14.1",
        "4.14.2",
        "4.14.3",
        "4.14.4",
        "4.14.5",
        "4.15.0",
        "4.16.0",
        "4.17.0",
        "4.18.0",
        "4.19.0",
        "4.20.0",
        "4.21.0",
        "4.22.0",
    }
    assert required <= catalog["releases"].keys()
    assert all(len(commit) == 40 for commit in catalog["releases"].values())
    assert all(values for values in catalog["hashes_by_path"].values())


def test_upgrade_backup_and_rollback_restore_previous_layout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    legacy = home / ".codex/skills/feature-spec/SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes((ROOT / "skills/feature-spec/SKILL.md").read_bytes())

    installed = run_setup(tmp_path, "--scope", "user")
    assert installed.returncode == 0, installed.stderr
    backup_id = next(line.split("=", 1)[1] for line in installed.stdout.splitlines() if line.startswith("backup_id="))
    assert not legacy.exists()

    rolled_back = run_setup(tmp_path, "--rollback", backup_id)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert legacy.read_bytes() == (ROOT / "skills/feature-spec/SKILL.md").read_bytes()
    assert not (home / ".agents/skills/feature-spec").exists()


def test_upgrade_rollback_restores_prior_gsd_version_manifests(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for root in (home / ".claude", home / ".codex"):
        (root / "gsd-core").mkdir(parents=True)
        (root / "gsd-core/VERSION").write_text("1.8.0\n")
        (root / "gsd-core/old.txt").write_text("old-owned\n")
        (root / "gsd-file-manifest.json").write_text(
            json.dumps(
                {
                    "version": "1.8.0",
                    "mode": "full",
                    "files": {"gsd-core/VERSION": "old", "gsd-core/old.txt": "old"},
                }
            )
        )

    installed = run_setup(tmp_path, "--scope", "user")
    assert installed.returncode == 0, installed.stderr
    backup_id = next(line.split("=", 1)[1] for line in installed.stdout.splitlines() if line.startswith("backup_id="))
    assert json.loads((home / ".codex/gsd-file-manifest.json").read_text())["version"] == "1.11.0"

    rolled_back = run_setup(tmp_path, "--rollback", backup_id)

    assert rolled_back.returncode == 0, rolled_back.stderr
    for root in (home / ".claude", home / ".codex"):
        assert json.loads((root / "gsd-file-manifest.json").read_text())["version"] == "1.8.0"
        assert (root / "gsd-core/VERSION").read_text() == "1.8.0\n"
        assert (root / "gsd-core/old.txt").read_text() == "old-owned\n"


def test_uninstall_is_explicit_and_preserves_edited_managed_copy(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    managed = tmp_path / "home/.agents/skills/fix/SKILL.md"
    managed.write_text("edited after install\n")

    result = run_setup(tmp_path, "--uninstall", "--scope", "user")

    assert result.returncode == 1
    assert managed.read_text() == "edited after install\n"
    assert "preserved" in result.stderr.lower()
    assert run_setup(tmp_path, "--uninstall").returncode == 2


def test_clean_uninstall_then_reinstall_is_supported(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    removed = run_setup(tmp_path, "--uninstall", "--scope", "user")
    assert removed.returncode == 0, removed.stderr
    assert not (tmp_path / "home/.agents/skills/feature-spec").exists()
    reinstalled = run_setup(tmp_path, "--scope", "user")
    assert reinstalled.returncode == 0, reinstalled.stderr
    assert (tmp_path / "home/.agents/skills/feature-spec/SKILL.md").is_file()


def test_unmanaged_user_skill_collision_fails_before_replacement(tmp_path: Path) -> None:
    collision = tmp_path / "home/.agents/skills/fix/SKILL.md"
    collision.parent.mkdir(parents=True)
    collision.write_text("mine\n")

    result = run_setup(tmp_path, "--scope", "user")

    assert result.returncode == 1
    assert collision.read_text() == "mine\n"
    assert "preserved" in result.stderr.lower()
    assert not (tmp_path / "home/.claude/skills/feature-spec").exists()


def test_broken_managed_link_is_repaired(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    link = project / ".agents/skills/feature-spec"
    link.parent.mkdir(parents=True)
    link.symlink_to("../../missing/feature-spec")

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 0, result.stderr
    assert link.resolve() == project / ".feature-fix-swarm/vendor/skills/feature-spec"


def test_linked_worktree_resolves_same_git_common_lock(tmp_path: Path) -> None:
    main = tmp_path / "main"
    init_repo(main)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True)
    (main / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(main), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "init"], check=True)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(linked), "-b", "linked"], check=True)

    script = (
        "from pathlib import Path; from lib.ffs_installer import project_lock_path; "
        "import sys; print(project_lock_path(Path(sys.argv[1])))"
    )
    first = subprocess.check_output([sys.executable, "-c", script, str(main)], cwd=ROOT, text=True).strip()
    second = subprocess.check_output([sys.executable, "-c", script, str(linked)], cwd=ROOT, text=True).strip()
    assert first == second
    assert first.endswith("feature-fix-swarm.setup.lock")


def test_project_manifest_cannot_escape_checkout_on_uninstall(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    manifest = project / ".feature-fix-swarm/install-manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "schema": "ffs.install/v1",
                "scope": "project",
                "paths": {"../victim": {"fingerprint": "file:forged"}},
            }
        )
    )

    result = run_setup(
        tmp_path, "--uninstall", "--scope", "project", "--project-dir", str(project)
    )

    assert result.returncode == 1
    assert "unsafe project manifest path" in result.stderr
    assert victim.read_text() == "keep\n"


def test_project_install_refuses_symlinked_discovery_ancestor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".agents").symlink_to(outside, target_is_directory=True)

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 1
    assert "symlinked ancestor" in result.stderr
    assert list(outside.iterdir()) == []


def test_project_install_refuses_ancestor_swap_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv("FFS_SKIP_SOCRATIC", "1")
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))
    real_replace_tree = ffs_installer.replace_tree
    swapped = False

    def swap_then_replace(*args: object, **kwargs: object) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            (project / ".feature-fix-swarm").symlink_to(
                outside, target_is_directory=True
            )
        real_replace_tree(*args, **kwargs)

    monkeypatch.setattr(ffs_installer, "replace_tree", swap_then_replace)

    with pytest.raises(
        ffs_installer.ActionableError,
        match="unsafe project destination|symlinked ancestor|changed during rollback",
    ):
        ffs_installer.install(ROOT, "project", project)

    assert list(outside.iterdir()) == []


def test_project_install_preserves_destination_created_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv("FFS_SKIP_SOCRATIC", "1")
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))
    real_replace_tree = ffs_installer.replace_tree
    injected: Path | None = None

    def inject_then_replace(
        source: Path, destination: Path, *args: object, **kwargs: object
    ) -> None:
        nonlocal injected
        if injected is None:
            destination.mkdir(parents=True)
            injected = destination / "operator.txt"
            injected.write_text("preserve me\n")
        real_replace_tree(source, destination, *args, **kwargs)

    monkeypatch.setattr(ffs_installer, "replace_tree", inject_then_replace)

    with pytest.raises(ffs_installer.ActionableError, match="changed during install"):
        ffs_installer.install(ROOT, "project", project)

    assert injected is not None
    assert injected.read_text() == "preserve me\n"


def test_project_install_adopts_single_collision_without_blocking_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A single edited/unmanaged collision must not hard-block every other
    skill by default, and --adopt-collisions must resolve just that one path
    (backed up) while leaving the rest of the install to proceed normally."""
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv("FFS_SKIP_SOCRATIC", "1")
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))

    assert ffs_installer.install(ROOT, "project", project) == 0

    skill_names = list(ffs_installer.source_skills(ROOT))
    collided_skill, untouched_skill = skill_names[0], skill_names[1]
    # Collision detection operates on the whole vendored skill directory (the
    # unit the manifest tracks), so edit one file inside it.
    collided_dir = project / ".feature-fix-swarm/vendor/skills" / collided_skill
    collided_path = collided_dir / "SKILL.md"
    original_text = collided_path.read_text()
    collided_path.write_text("locally edited\n")

    # RED: today, ONE colliding path hard-blocks the entire install, even for
    # skills that never changed.
    with pytest.raises(
        ffs_installer.ActionableError,
        match="preserved edited/unmanaged collision",
    ):
        ffs_installer.install(ROOT, "project", project)
    assert collided_path.read_text() == "locally edited\n"

    capsys.readouterr()
    exit_code = ffs_installer.install(ROOT, "project", project, adopt_collisions=True)
    assert exit_code == 0

    out = capsys.readouterr().out
    assert f"adopted collision: {collided_dir}" in out
    assert "adopted_collisions=1" in out

    # The collided skill was restored to the vendor bytes...
    assert collided_path.read_text() == original_text
    # ...and the untouched skill still installed correctly.
    untouched_link = project / ".claude/skills" / untouched_skill
    assert untouched_link.is_symlink()

    # ...and the local edit is recoverable from the printed backup path.
    backup_line = next(line for line in out.splitlines() if "adopted collision:" in line)
    backup_path = Path(backup_line.rsplit("backed up at ", 1)[1])
    assert (backup_path / "SKILL.md").read_text() == "locally edited\n"


def test_project_entry_replacement_preserves_creation_during_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    destination = project / ".agents/skills/example"
    backup = ffs_installer.Backup("install", "project", project)

    def materialize(temporary: Path, _parent_fd: int) -> None:
        temporary.symlink_to("../../vendor/example")
        Path(destination.name).write_text("preserve me\n")

    with pytest.raises(ffs_installer.ActionableError, match="changed during install"):
        ffs_installer.replace_project_entry(
            destination,
            backup,
            project,
            "missing",
            materialize,
        )

    assert destination.read_text() == "preserve me\n"


def test_project_no_replace_rename_preserves_existing_destination(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    source = parent / "source"
    source.write_text("new\n")
    destination = parent / "destination"
    destination.write_text("preserve me\n")
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(FileExistsError):
            ffs_installer.rename_no_replace(
                source.name,
                destination.name,
                source_fd=directory_fd,
                destination_fd=directory_fd,
            )
    finally:
        os.close(directory_fd)

    assert source.read_text() == "new\n"
    assert destination.read_text() == "preserve me\n"


def test_project_manifest_write_refuses_post_write_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv("FFS_SKIP_SOCRATIC", "1")
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))
    real_replace_link = ffs_installer.replace_link
    last_skill = max(ffs_installer.source_skills(ROOT))
    swapped = False

    def swap_after_last_link(*args: object, **kwargs: object) -> None:
        nonlocal swapped
        real_replace_link(*args, **kwargs)
        destination = args[0]
        assert isinstance(destination, Path)
        if (
            not swapped
            and destination.name == last_skill
            and destination.parent == project / ".claude/skills"
        ):
            swapped = True
            anchor = project / ".feature-fix-swarm"
            anchor.rename(project / ".feature-fix-swarm-held")
            anchor.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(ffs_installer, "replace_link", swap_after_last_link)

    with pytest.raises(
        ffs_installer.ActionableError,
        match="unsafe project destination|symlinked ancestor|changed during rollback",
    ):
        ffs_installer.install(ROOT, "project", project)

    assert list(outside.iterdir()) == []


def test_partial_upstream_gsd_failure_restores_preexisting_namespace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old_agent = home / ".claude/agents/gsd-old.md"
    old_agent.parent.mkdir(parents=True)
    old_agent.write_text("old bytes\n")
    old_manifest = home / ".claude/gsd-file-manifest.json"
    old_manifest.write_text(
        json.dumps(
            {
                "version": "1.8.0",
                "mode": "full",
                "files": {"agents/gsd-old.md": "old"},
            }
        )
    )

    result = run_setup(
        tmp_path,
        "--scope",
        "user",
        extra_env={"FFS_GSD_STUB_FAIL_RUNTIME": "codex"},
    )

    assert result.returncode == 1
    assert old_agent.read_text() == "old bytes\n"
    assert json.loads(old_manifest.read_text())["version"] == "1.8.0"
    assert not (home / ".codex/gsd-file-manifest.json").exists()


def test_truncated_failure_manifest_cannot_bypass_upstream_restore(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old_manifest = home / ".codex/gsd-file-manifest.json"
    old_manifest.parent.mkdir(parents=True)
    old_manifest.write_text(
        json.dumps({"version": "1.8.0", "mode": "full", "files": {}})
    )

    result = run_setup(
        tmp_path,
        "--scope",
        "user",
        extra_env={
            "FFS_GSD_STUB_FAIL_RUNTIME": "codex",
            "FFS_GSD_STUB_CORRUPT_ON_FAILURE": "1",
        },
    )

    assert result.returncode == 1
    assert json.loads(old_manifest.read_text())["version"] == "1.8.0"


def test_project_legacy_migration_refuses_symlinked_codex_ancestor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    legacy = outside / "skills/feature-spec"
    legacy.mkdir(parents=True)
    legacy_file = legacy / "SKILL.md"
    legacy_file.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    (project / ".codex").symlink_to(outside, target_is_directory=True)

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 1
    assert "symlinked ancestor" in result.stderr
    assert legacy_file.is_file()
    # The failure happens after the upstream installer; the outer transaction
    # must still put both global GSD profiles back to their prior absence.
    assert not (tmp_path / "home/.claude/gsd-file-manifest.json").exists()
    assert not (tmp_path / "home/.codex/gsd-file-manifest.json").exists()


def test_backup_creation_refuses_symlink_race_before_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    real_mkdir = os.mkdir
    raced = False

    def plant_backup_symlink(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal raced
        if not raced and path == "backups" and dir_fd is not None:
            raced = True
            (home / ".cache/feature-fix-swarm/backups").symlink_to(
                outside, target_is_directory=True
            )
            raise FileExistsError(path)
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", plant_backup_symlink)

    with pytest.raises(ffs_installer.ActionableError, match="private backup root"):
        ffs_installer.Backup("install", "user")

    assert raced
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode
    assert list(outside.iterdir()) == []


def test_private_cache_supports_an_intentional_cache_parent_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cache_target = tmp_path / "cache-target"
    cache_target.mkdir()
    (home / ".cache").symlink_to(cache_target, target_is_directory=True)
    backup = ffs_installer.Backup("install", "user")
    source = tmp_path / "source.txt"
    source.write_text("private payload\n")

    backup.before(source)
    backup.finish()

    assert backup.directory.resolve().is_relative_to(cache_target.resolve())
    assert (backup.directory / "objects/0000").read_text() == "private payload\n"


def test_backup_supports_an_intentional_source_parent_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    claude_target = tmp_path / "claude-target"
    claude_target.mkdir()
    (home / ".claude").symlink_to(claude_target, target_is_directory=True)
    source = home / ".claude/config.json"
    source.write_text('{"private":"payload"}\n')
    backup = ffs_installer.Backup("install", "user")

    backup.before(source)
    backup.finish()

    assert (backup.directory / "objects/0000").read_text() == source.read_text()


def test_private_json_has_restrictive_mode_before_content_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "private.json"
    real_dump = json.dump
    observed_mode: int | None = None

    def inspect_mode(value: object, handle: object, *args: object, **kwargs: object) -> None:
        nonlocal observed_mode
        observed_mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        real_dump(value, handle, *args, **kwargs)

    monkeypatch.setattr(json, "dump", inspect_mode)

    ffs_installer.write_json_file(destination, {"token": "redacted"}, mode=0o600)

    assert observed_mode == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_backup_payload_writes_stay_on_open_directory_after_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    backup = ffs_installer.Backup("install", "user")
    held = backup.directory.with_name(f"{backup.directory.name}-held")
    backup.directory.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    backup.directory.symlink_to(outside, target_is_directory=True)
    source = tmp_path / "source.txt"
    source.write_text("private payload\n")

    backup.before(source)
    backup.finish()

    assert (held / "objects/0000").read_text() == "private payload\n"
    assert (held / "manifest.json").is_file()
    assert list(outside.iterdir()) == []


def test_rollback_refuses_symlinked_backup_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "source.txt"
    source.write_text("private payload\n")
    backup = ffs_installer.Backup("install", "user")
    backup.before(source)
    backup.finish()
    held = backup.directory.with_name(f"{backup.directory.name}-held")
    backup.directory.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    backup.directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ffs_installer.ActionableError, match="backup not found or unsafe"):
        ffs_installer.rollback(backup.backup_id)

    assert list(outside.iterdir()) == []


def test_backup_source_swap_to_symlink_is_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "source.txt"
    source.write_text("expected bytes\n")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be copied\n")
    backup = ffs_installer.Backup("install", "user")
    real_open = os.open
    swapped = False

    def swap_before_source_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and path == source.name
            and flags & getattr(os, "O_NOFOLLOW", 0)
            and not flags & getattr(os, "O_DIRECTORY", 0)
            and kwargs.get("dir_fd") is not None
        ):
            swapped = True
            source.unlink()
            source.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_source_open)

    with pytest.raises(ffs_installer.ActionableError, match="source changed"):
        backup.before(source)

    assert swapped
    copied = backup.directory / "objects/0000"
    assert not copied.exists()
    assert outside.read_text() == "must not be copied\n"
    os.close(backup.directory_fd)


def test_project_legacy_migration_refuses_symlinked_final_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_repo(project)
    outside = tmp_path / "outside"
    legacy = outside / "feature-spec"
    legacy.mkdir(parents=True)
    legacy_file = legacy / "SKILL.md"
    legacy_file.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    codex = project / ".codex"
    codex.mkdir()
    (codex / "skills").symlink_to(outside, target_is_directory=True)

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 1
    assert "symlinked project directory" in result.stderr
    assert legacy_file.is_file()


def test_user_legacy_migration_refuses_symlinked_final_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    outside = tmp_path / "outside"
    legacy = outside / "feature-spec"
    legacy.mkdir(parents=True)
    legacy_file = legacy / "SKILL.md"
    legacy_file.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    codex = home / ".codex"
    codex.mkdir()
    (codex / "skills").symlink_to(outside, target_is_directory=True)
    backup = ffs_installer.Backup("install", "user")

    with pytest.raises(ffs_installer.ActionableError, match="unsafe legacy skill root"):
        ffs_installer.migrate_legacy(ROOT, [codex / "skills"], backup)

    assert legacy_file.is_file()


def test_user_legacy_rollback_refuses_post_migration_root_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    legacy_root = home / ".codex/skills"
    legacy = legacy_root / "feature-spec/SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    backup = ffs_installer.Backup("install", "user")

    preserved = ffs_installer.migrate_legacy(ROOT, [legacy_root], backup)

    assert preserved == []
    assert not legacy.exists()
    held = home / ".codex/skills-held"
    legacy_root.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    legacy_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ffs_installer.ActionableError, match="managed path changed"):
        backup.restore_uncommitted()

    assert not (outside / "feature-spec/SKILL.md").exists()
    assert any((backup.directory / "objects").iterdir())


def test_project_legacy_migration_refuses_child_swap_to_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    legacy = project / ".codex/skills/feature-spec"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(
        (ROOT / "skills/feature-spec/SKILL.md").read_text()
    )
    outside = tmp_path / "outside/feature-spec"
    outside.mkdir(parents=True)
    outside_file = outside / "SKILL.md"
    outside_file.write_text((ROOT / "skills/feature-spec/SKILL.md").read_text())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    backup = ffs_installer.Backup("install", "project", project)
    real_open = os.open
    swapped = False

    def swap_before_child_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and path == "feature-spec"
            and flags & getattr(os, "O_DIRECTORY", 0)
            and kwargs.get("dir_fd") is not None
        ):
            swapped = True
            legacy.rename(project / ".codex/skills/feature-spec-held")
            legacy.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_child_open)

    with pytest.raises(ffs_installer.ActionableError, match="legacy skill changed"):
        ffs_installer.migrate_legacy(
            ROOT,
            [project / ".codex/skills"],
            backup,
            project=project,
        )

    assert swapped
    assert outside_file.is_file()


def test_failure_after_first_ffs_write_restores_gsd_and_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv("FFS_SKIP_SOCRATIC", "1")
    monkeypatch.setenv("FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py"))
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))

    real_replace_tree = ffs_installer.replace_tree
    failed = False

    def fail_after_write(
        source: Path,
        destination: Path,
        backup: ffs_installer.Backup,
        **kwargs: object,
    ) -> None:
        nonlocal failed
        real_replace_tree(source, destination, backup, **kwargs)
        if not failed and str(destination).startswith(str(project)):
            failed = True
            raise RuntimeError("synthetic post-GSD failure")

    monkeypatch.setattr(ffs_installer, "replace_tree", fail_after_write)

    with pytest.raises(RuntimeError, match="synthetic post-GSD failure"):
        ffs_installer.install(ROOT, "project", project)

    assert list((project / ".feature-fix-swarm/vendor/skills").glob("*")) == []
    assert not (home / ".claude/gsd-file-manifest.json").exists()
    assert not (home / ".codex/gsd-file-manifest.json").exists()


def test_failure_rollback_preserves_concurrent_project_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    init_repo(project)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("FFS_SKIP_PROMPT_MASTER", "1")
    monkeypatch.setenv("FFS_SKIP_SOCRATIC", "1")
    monkeypatch.setenv(
        "FFS_GSD_INSTALLER", str(ROOT / "tests/fixtures/gsd-installer-stub.py")
    )
    monkeypatch.setenv("FFS_GSD_STUB_LOG", str(tmp_path / "gsd-installer.log"))
    real_replace_tree = ffs_installer.replace_tree
    changed: Path | None = None

    def change_then_fail(
        source: Path,
        destination: Path,
        backup: ffs_installer.Backup,
        **kwargs: object,
    ) -> None:
        nonlocal changed
        real_replace_tree(source, destination, backup, **kwargs)
        if changed is None and str(destination).startswith(str(project)):
            ffs_installer.remove_path(destination)
            destination.mkdir()
            changed = destination / "concurrent.txt"
            changed.write_text("preserve me\n")
            raise RuntimeError("synthetic concurrent change")

    monkeypatch.setattr(ffs_installer, "replace_tree", change_then_fail)

    with pytest.raises(ffs_installer.ActionableError, match="changed during rollback"):
        ffs_installer.install(ROOT, "project", project)

    assert changed is not None
    assert changed.read_text() == "preserve me\n"
    assert not (home / ".claude/gsd-file-manifest.json").exists()
    assert not (home / ".codex/gsd-file-manifest.json").exists()


def test_project_install_hints_ffs_init_when_registry_absent(tmp_path: Path) -> None:
    # Seam 6 (REQ-401): one stdout hint line when the install-target project
    # lacks config/environments.yaml; exit code unchanged (clean install = 0).
    project = tmp_path / "project"
    init_repo(project)

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 0, result.stderr
    hint_lines = [l for l in result.stdout.splitlines() if l.startswith("hint:")]
    assert len(hint_lines) == 1, result.stdout
    assert "/ffs-init" in hint_lines[0]
    assert "config/environments.yaml" in hint_lines[0]


def test_project_install_no_hint_when_registry_present(tmp_path: Path) -> None:
    # Presence-only check: any file content suppresses the hint; rc unchanged.
    project = tmp_path / "project"
    init_repo(project)
    registry = project / "config" / "environments.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text("environments: []\n")

    result = run_setup(tmp_path, "--scope", "project", "--project-dir", str(project))

    assert result.returncode == 0, result.stderr
    assert "/ffs-init" not in result.stdout


# --- managed lib runtime delivery (gates.py was previously undeliverable) ---

MANAGED_LIB_EXPECTED = {
    "lib/gates.py": ".claude/lib/feature-fix-swarm/gates.py",
    "lib/runtime_proof.py": ".claude/lib/feature-fix-swarm/runtime_proof.py",
    "scripts/gsd/socratic-slice.sh": ".claude/lib/feature-fix-swarm/scripts/gsd/socratic-slice.sh",
}


def test_user_install_stages_managed_lib_runtime(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "--scope", "user")

    assert result.returncode == 0, result.stderr
    home = tmp_path / "home"
    manifest = json.loads((home / ".cache/feature-fix-swarm/install-manifest.json").read_text())
    for source_rel, dest_rel in MANAGED_LIB_EXPECTED.items():
        staged = home / dest_rel
        assert staged.is_file() and not staged.is_symlink(), dest_rel
        assert staged.read_bytes() == (ROOT / source_rel).read_bytes(), dest_rel
        assert str(staged) in manifest["paths"], dest_rel
    # the skill ladders exec socratic-slice.sh directly (no interpreter prefix);
    # gates.py is always python3-prefixed, so only the script needs the x bit
    assert (home / ".claude/lib/feature-fix-swarm/scripts/gsd/socratic-slice.sh").stat().st_mode & 0o100


def test_doctor_flags_stale_managed_gates(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    staged = tmp_path / "home/.claude/lib/feature-fix-swarm/gates.py"
    staged.write_text("# stale drifted copy\n")

    report = json.loads(run_setup(tmp_path, "--doctor", "--scope", "user", "--json").stdout)
    drifted = [
        check
        for check in report["checks"]
        if check["id"] == "managed-path" and check["status"] == "fail" and "gates.py" in check["message"]
    ]
    assert drifted, report["checks"]


def test_uninstall_removes_managed_lib_runtime(tmp_path: Path) -> None:
    assert run_setup(tmp_path, "--scope", "user").returncode == 0
    staged = tmp_path / "home/.claude/lib/feature-fix-swarm/gates.py"
    assert staged.is_file()

    result = run_setup(tmp_path, "--uninstall", "--scope", "user")
    assert result.returncode == 0, result.stderr
    assert not staged.exists()
